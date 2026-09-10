import { useMemo, useState } from "react";

import { useUiText } from "../../i18n/useUiText";
import { buildPaperDisplayLabels } from "../../utils/paperLabels";

export { buildPaperDisplayLabels } from "../../utils/paperLabels";

export type OutlinePaper = {
  paper_id: string;
  title?: unknown;
  keywords?: string[];
  abstract?: string;
};

export type OutlineSectionDraft = {
  sectionId?: string;
  headingLevel?: number;
  title: string;
  purpose: string;
  paperIds: string[];
  contextPaperIds?: string[];
  notes: string;
  sectionRole?: "introduction" | "body" | "conclusion" | "references";
};

export type VisualOutlineDraft = {
  preamble: string;
  sections: OutlineSectionDraft[];
};

export type OutlinePaperRecommendation = {
  section_index: number;
  paper_ids: string[];
};

const EMPTY_TITLE_SENTINEL = "<!-- outline-untitled -->";

function unique(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

export function parseOutlineMarkdown(value: string): VisualOutlineDraft {
  const preamble: string[] = [];
  const sections: OutlineSectionDraft[] = [];
  const explicitIds = new Set<OutlineSectionDraft>();
  let current: OutlineSectionDraft | null = null;
  for (const rawLine of String(value || "").replace(/\r\n?/g, "\n").split("\n")) {
    const heading = rawLine.trim().match(/^(#{2,6})\s+(?:\d+(?:\.\d+)*[.)]?\s+)?(.+?)\s*$/);
    if (heading) {
      const parsedTitle = heading[2].trim();
      current = { sectionId: `S${String(sections.length + 1).padStart(2, "0")}`, headingLevel: heading[1].length, title: parsedTitle === EMPTY_TITLE_SENTINEL ? "" : parsedTitle, purpose: "", paperIds: [], contextPaperIds: [], notes: "" };
      sections.push(current);
      continue;
    }
    if (!current) {
      preamble.push(rawLine);
      continue;
    }
    const identity = rawLine.trim().match(/^<!-- section_id: (S[A-Za-z0-9_-]{1,64}) -->$/);
    if (identity) { current.sectionId = identity[1]; explicitIds.add(current); continue; }
    const assigned = rawLine.trim().match(/^Assigned papers:\s*(.*)$/i);
    if (assigned) {
      current.paperIds = unique(assigned[1].replace(/[.。]\s*$/, "").split(/[,，;；]/));
      continue;
    }
    const contextual = rawLine.trim().match(/^(?:Context|Contextual) papers:\s*(.*)$/i);
    if (contextual) {
      current.contextPaperIds = unique(contextual[1].replace(/[.。]\s*$/, "").split(/[,，;；]/));
      continue;
    }
    const role = rawLine.trim().match(/^Section role:\s*(introduction|body|conclusion|references)\s*$/i);
    if (role) {
      current.sectionRole = role[1].toLowerCase() as OutlineSectionDraft["sectionRole"];
      continue;
    }
    const purpose = rawLine.trim().match(/^Purpose:\s*(.*)$/i);
    if (purpose && !current.purpose) {
      current.purpose = purpose[1].trim();
      continue;
    }
    current.notes = [current.notes, rawLine].filter(Boolean).join("\n");
  }
  const used = new Set([...explicitIds].map(section => section.sectionId));
  let serial = 1;
  for (const section of sections) {
    if (explicitIds.has(section)) continue;
    while (used.has(`S${String(serial).padStart(2, "0")}`)) serial++;
    section.sectionId = `S${String(serial).padStart(2, "0")}`;
    used.add(section.sectionId);
  }
  return { preamble: preamble.join("\n").trim(), sections };
}

export function serializeOutlineMarkdown(draft: VisualOutlineDraft): string {
  const preamble = draft.preamble.trim() || "# Selected Outline\n\nPrimary structure: user-edited visual outline.";
  const used = new Set(draft.sections.map(section => section.sectionId).filter(Boolean));
  let serial = 1;
  const blocks = draft.sections.map((section, index) => {
    while (used.has(`S${String(serial).padStart(2, "0")}`)) serial++;
    const sectionId = section.sectionId || `S${String(serial).padStart(2, "0")}`;
    used.add(sectionId);
    const title = section.title.trim() || EMPTY_TITLE_SENTINEL;
    const purpose = section.purpose.trim() || "Synthesize and compare the assigned Matrix evidence.";
    const lines = [
      `${"#".repeat(section.headingLevel || 2)} ${index + 1}. ${title}`,
      `<!-- section_id: ${sectionId} -->`,
    ];
    if (section.sectionRole) lines.push(`Section role: ${section.sectionRole}`);
    if (section.paperIds.length) lines.push(`Assigned papers: ${unique(section.paperIds).join(", ")}.`);
    if (section.contextPaperIds?.length) lines.push(`Context papers: ${unique(section.contextPaperIds).join(", ")}.`);
    lines.push(`Purpose: ${purpose}`);
    if (section.notes.trim()) lines.push(section.notes.trim());
    return lines.join("\n");
  });
  return [preamble, ...blocks].join("\n\n").trim() + "\n";
}

export function validateVisualOutline(draft: VisualOutlineDraft) {
  const missingTitles = draft.sections.flatMap((section, index) => section.title.trim() ? [] : [index + 1]);
  return {
    sectionCount: draft.sections.length,
    missingTitles,
    ready: draft.sections.length > 0 && !missingTitles.length,
  };
}

export function applyOutlinePaperRecommendations(value: string, recommendations: OutlinePaperRecommendation[]): string {
  const draft = parseOutlineMarkdown(value);
  const papersBySection = new Map(recommendations.map((item) => [item.section_index, unique(item.paper_ids)]));
  return serializeOutlineMarkdown({
    ...draft,
    sections: draft.sections.map((section, index) => papersBySection.has(index)
      ? { ...section, paperIds: papersBySection.get(index) || [] }
      : section),
  });
}

function paperText(paper: OutlinePaper): string {
  const title = typeof paper.title === "string" ? paper.title : JSON.stringify(paper.title || "");
  return [paper.paper_id, title, ...(paper.keywords || []), paper.abstract || ""].join(" ").toLowerCase();
}

export function OutlineBuilder({ value, papers, onChange }: { value: string; papers: OutlinePaper[]; onChange: (value: string) => void }) {
  const { text } = useUiText();
  const [mode, setMode] = useState<"visual" | "markdown">("visual");
  const [paperFilters, setPaperFilters] = useState<Record<number, string>>({});
  const draft = useMemo(() => parseOutlineMarkdown(value), [value]);
  const paperLabels = useMemo(() => buildPaperDisplayLabels(papers), [papers]);
  const validation = validateVisualOutline(draft);

  function commit(next: VisualOutlineDraft) {
    onChange(serializeOutlineMarkdown(next));
  }

  function updateSection(index: number, update: Partial<OutlineSectionDraft>) {
    commit({ ...draft, sections: draft.sections.map((section, sectionIndex) => sectionIndex === index ? { ...section, ...update } : section) });
  }

  function addSection(title = "") {
    commit({ ...draft, sections: [...draft.sections, { title, purpose: "", paperIds: [], contextPaperIds: [], notes: "" }] });
  }

  function addStarterSections() {
    const introduction: OutlineSectionDraft = { title: text("引言与范围", "Introduction and scope"), purpose: text("说明综述范围、术语和组织问题。", "Define the review scope, terminology, and organizing question."), paperIds: [], contextPaperIds: [], notes: "", sectionRole: "introduction" };
    const conclusion: OutlineSectionDraft = { title: text("结论与展望", "Conclusion and outlook"), purpose: text("比较主要证据、局限与未来方向。", "Compare the main evidence, limitations, and future directions."), paperIds: [], contextPaperIds: [], notes: "", sectionRole: "conclusion" };
    const hasIntroduction = draft.sections.some((section) => section.sectionRole === "introduction" || /^(?:introduction|引言)/i.test(section.title.trim()));
    const hasConclusion = draft.sections.some((section) => section.sectionRole === "conclusion" || /^(?:conclusion|结论)/i.test(section.title.trim()));
    commit({
      ...draft,
      sections: [
        ...(hasIntroduction ? [] : [introduction]),
        ...draft.sections,
        ...(hasConclusion ? [] : [conclusion]),
      ],
    });
  }

  function move(index: number, offset: number) {
    const target = index + offset;
    if (target < 0 || target >= draft.sections.length) return;
    const sections = [...draft.sections];
    [sections[index], sections[target]] = [sections[target], sections[index]];
    commit({ ...draft, sections });
  }

  if (mode === "markdown") {
    return (
      <div className="beginner-outline-editor">
        <div className="outline-mode-switch"><button type="button" onClick={() => setMode("visual")}>{text("新手填写", "Beginner editor")}</button><button type="button" className="active" onClick={() => setMode("markdown")}>{text("高级 Markdown", "Advanced Markdown")}</button></div>
        <textarea className="outline-editor" value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} />
        <p className="muted">{text("每个 ## 章节需填写标题；暂无合适论文时可省略 Assigned papers: 行。", "Give every ## section a title; omit Assigned papers: when no suitable papers are available.")}</p>
      </div>
    );
  }

  return (
    <div className="beginner-outline-editor">
      <div className="outline-mode-switch"><button type="button" className="active" onClick={() => setMode("visual")}>{text("新手填写", "Beginner editor")}</button><button type="button" onClick={() => setMode("markdown")}>{text("高级 Markdown", "Advanced Markdown")}</button></div>
      <div className="outline-builder-toolbar"><div><strong>{text("按章节填写，无需了解 Markdown", "Fill in sections without learning Markdown")}</strong><p>{text("填写标题和写作目标后可统一推荐论文；允许暂时不选论文，推荐结果可手动调整。", "Fill in titles and goals, then recommend papers in one pass; paper selections are optional and editable.")}</p></div><div><button className="button button-secondary" type="button" onClick={addStarterSections}>{text("加入引言和结论", "Add introduction and conclusion")}</button><button className="button button-primary" type="button" onClick={() => addSection()}>{text("添加章节", "Add section")}</button></div></div>
      <div className={validation.ready ? "outline-builder-validation ready" : "outline-builder-validation"}>{validation.sectionCount ? text(`${validation.sectionCount} 个章节 · ${validation.missingTitles.length} 个缺少标题`, `${validation.sectionCount} sections · ${validation.missingTitles.length} missing titles`) : text("还没有章节，请添加章节或使用引言/结论模板。", "No sections yet. Add a section or use the introduction/conclusion starter.")}</div>
      {draft.sections.length ? <div className="outline-builder-list">{draft.sections.map((section, index) => {
        const filter = (paperFilters[index] || "").toLowerCase();
        const visible = papers.filter((paper) => `${paperText(paper)} ${paperLabels.get(paper.paper_id) || ""}`.includes(filter));
        return <article className="outline-builder-card" key={`outline-section-${index}`}><div className="outline-builder-card-head"><strong>{text(`第 ${index + 1} 节`, `Section ${index + 1}`)}</strong><div><button type="button" className="button button-quiet" disabled={index === 0} onClick={() => move(index, -1)}>↑</button><button type="button" className="button button-quiet" disabled={index === draft.sections.length - 1} onClick={() => move(index, 1)}>↓</button><button type="button" className="button button-quiet danger" onClick={() => commit({ ...draft, sections: draft.sections.filter((_, sectionIndex) => sectionIndex !== index) })}>{text("删除", "Delete")}</button></div></div>
          <label className="outline-builder-field"><span>{text("章节标题", "Section title")}</span><input value={section.title} onChange={(event) => updateSection(index, { title: event.target.value })} placeholder={text("例如：芳香族底物的反应范围", "e.g. Scope of aromatic substrates")} /></label>
          <label className="outline-builder-field"><span>{text("章节层级", "Heading level")}</span><select value={section.headingLevel || 2} onChange={(event) => updateSection(index, { headingLevel: Number(event.target.value) })}>
            {[2, 3, 4, 5, 6].map(level => <option key={level} value={level} disabled={index === 0 && level !== 2}>{level === 2 ? text("一级章节", "Main section") : text(`${level - 1} 级小节（属于前面的上级章节）`, `Level ${level - 1} subsection (under the preceding parent)`)}</option>)}
          </select></label>
          <label className="outline-builder-field"><span>{text("本节要回答什么问题", "What should this section answer?")}</span><textarea value={section.purpose} onChange={(event) => updateSection(index, { purpose: event.target.value })} placeholder={text("说明本节比较哪些工作、解决什么问题。", "Describe the papers and question this section should compare.")} /></label>
          {section.contextPaperIds?.length ? <p className="outline-context-note">{text(`系统已将 ${section.contextPaperIds.length} 篇综述或观点文献作为背景证据，不计入正文主分类。`, `${section.contextPaperIds.length} review or perspective paper(s) are retained as contextual evidence rather than primary body evidence.`)}</p> : null}
          <details className="outline-paper-picker"><summary>{text(`选择论文（已选 ${section.paperIds.length} 篇）`, `Select papers (${section.paperIds.length} selected)`)}</summary><div className="outline-paper-picker-body"><input type="search" value={paperFilters[index] || ""} onChange={(event) => setPaperFilters((current) => ({ ...current, [index]: event.target.value }))} placeholder={text("按短序号或标题筛选", "Filter by short number or title")} /><div className="outline-paper-options">{visible.map((paper) => <label key={paper.paper_id} title={text(`内部论文 ID：${paper.paper_id}`, `Internal paper ID: ${paper.paper_id}`)}><input type="checkbox" checked={section.paperIds.includes(paper.paper_id)} onChange={(event) => updateSection(index, { paperIds: event.target.checked ? unique([...section.paperIds, paper.paper_id]) : section.paperIds.filter((paperId) => paperId !== paper.paper_id) })} /><span><strong>{paperLabels.get(paper.paper_id) || paper.paper_id}</strong> · {typeof paper.title === "string" ? paper.title : JSON.stringify(paper.title || "")}</span></label>)}</div></div></details>
          <label className="outline-builder-field"><span>{text("补充要求（可选）", "Additional instructions (optional)")}</span><textarea value={section.notes} onChange={(event) => updateSection(index, { notes: event.target.value })} placeholder={text("例如比较规则、机理重点、图表计划或段落衔接。", "Optional comparison rules, mechanism focus, figures, or transitions.")} /></label>
        </article>;
      })}</div> : <div className="outline-builder-empty"><strong>{text("从一个空白的可视化大纲开始", "Start with a blank visual outline")}</strong><p>{text("点击“添加章节”，像填写表单一样完成大纲。", "Click Add section and complete the outline like a form.")}</p><button className="button button-primary" type="button" onClick={() => addSection()}>{text("添加第一个章节", "Add first section")}</button></div>}
    </div>
  );
}
