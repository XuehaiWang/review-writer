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

export type OutlineTreeNode = { index: number; end: number; number: string; children: OutlineTreeNode[] };

export type OutlinePaperRecommendation = {
  section_index: number;
  paper_ids: string[];
};

const EMPTY_TITLE_SENTINEL = "<!-- outline-untitled -->";

function unique(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

export function buildOutlineTree(sections: OutlineSectionDraft[]): { roots: OutlineTreeNode[]; valid: boolean } {
  const roots: OutlineTreeNode[] = [];
  const stack: Array<{ level: number; node: OutlineTreeNode }> = [];
  let valid = true;
  sections.forEach((section, index) => {
    const level = section.headingLevel || 2;
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    if (level < 2 || level > 6 || (stack.length ? level !== stack[stack.length - 1].level + 1 : level !== 2)) valid = false;
    const siblings = stack.length ? stack[stack.length - 1].node.children : roots;
    const number = `${stack.length ? `${stack[stack.length - 1].node.number}.` : ""}${siblings.length + 1}`;
    const node: OutlineTreeNode = { index, end: index + 1, number, children: [] };
    siblings.push(node);
    stack.forEach(({ node: ancestor }) => { ancestor.end = index + 1; });
    stack.push({ level, node });
  });
  return { roots, valid };
}

function findNode(roots: OutlineTreeNode[], index: number): { node: OutlineTreeNode; siblings: OutlineTreeNode[] } | null {
  const node = roots.find((item) => item.index === index);
  if (node) return { node, siblings: roots };
  for (const root of roots) {
    const found = findNode(root.children, index);
    if (found) return found;
  }
  return null;
}

export function moveOutlineSubtree(sections: OutlineSectionDraft[], index: number, offset: -1 | 1): OutlineSectionDraft[] {
  const found = findNode(buildOutlineTree(sections).roots, index);
  if (!found) return sections;
  const siblingIndex = found.siblings.indexOf(found.node);
  const sibling = found.siblings[siblingIndex + offset];
  if (!sibling) return sections;
  const first = offset < 0 ? sibling : found.node;
  const second = offset < 0 ? found.node : sibling;
  return [
    ...sections.slice(0, first.index),
    ...sections.slice(second.index, second.end),
    ...sections.slice(first.end, second.index),
    ...sections.slice(first.index, first.end),
    ...sections.slice(second.end),
  ];
}

export function changeOutlineLevel(sections: OutlineSectionDraft[], index: number, level: number): OutlineSectionDraft[] | null {
  const current = sections[index]?.headingLevel || 2;
  const node = findNode(buildOutlineTree(sections).roots, index)?.node;
  if (!node || level < 2 || level > 6) return null;
  if (level === current) return sections;
  const parentExists = level === 2 || sections.slice(0, index).some((section) => (section.headingLevel || 2) === level - 1);
  const nextLevel = sections[node.end]?.headingLevel || 2;
  if (!parentExists || node.end < sections.length && nextLevel > level) return null;
  const delta = level - current;
  if (sections.slice(index, node.end).some((section) => (section.headingLevel || 2) + delta > 6 || (section.headingLevel || 2) + delta < 2)) return null;
  const updated = sections.map((section, sectionIndex) => sectionIndex >= index && sectionIndex < node.end
    ? { ...section, headingLevel: (section.headingLevel || 2) + delta } : section);
  return buildOutlineTree(updated).valid ? updated : null;
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
  const preamble = draft.preamble.trim() || "# Selected Outline\n\nPrimary structure: user-edited outline.";
  const used = new Set(draft.sections.map(section => section.sectionId).filter(Boolean));
  const numbers = new Map<number, string>();
  const visit = (nodes: OutlineTreeNode[]) => nodes.forEach((node) => { numbers.set(node.index, node.number); visit(node.children); });
  visit(buildOutlineTree(draft.sections).roots);
  let serial = 1;
  const blocks = draft.sections.map((section, index) => {
    while (used.has(`S${String(serial).padStart(2, "0")}`)) serial++;
    const sectionId = section.sectionId || `S${String(serial).padStart(2, "0")}`;
    used.add(sectionId);
    const title = section.title.trim() || EMPTY_TITLE_SENTINEL;
    const purpose = section.purpose.trim() || "Synthesize and compare the assigned paper evidence.";
    const lines = [
      `${"#".repeat(section.headingLevel || 2)} ${numbers.get(index) || index + 1}. ${title}`,
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
  const [paperFilters, setPaperFilters] = useState<Record<string, string>>({});
  const draft = useMemo(() => parseOutlineMarkdown(value), [value]);
  const tree = useMemo(() => buildOutlineTree(draft.sections), [draft.sections]);
  const moduleEditable = tree.valid && !/^(?:[ \t]*(```|~~~)|#{7,}[ \t]|#{2,6}[ \t]*$)/m.test(value);
  const paperLabels = useMemo(() => buildPaperDisplayLabels(papers), [papers]);
  const validation = validateVisualOutline(draft);

  function commit(next: VisualOutlineDraft) {
    onChange(serializeOutlineMarkdown(next));
  }

  function updateSection(index: number, update: Partial<OutlineSectionDraft>) {
    commit({ ...draft, sections: draft.sections.map((section, sectionIndex) => sectionIndex === index ? { ...section, ...update } : section) });
  }

  function addSection(parent?: OutlineTreeNode) {
    const section: OutlineSectionDraft = { title: "", headingLevel: parent ? (draft.sections[parent.index].headingLevel || 2) + 1 : 2, purpose: "", paperIds: [], contextPaperIds: [], notes: "" };
    if (section.headingLevel! > 6) return;
    const sections = [...draft.sections];
    sections.splice(parent?.end ?? sections.length, 0, section);
    commit({ ...draft, sections });
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
    const sections = moveOutlineSubtree(draft.sections, index, offset as -1 | 1);
    if (sections !== draft.sections) commit({ ...draft, sections });
  }

  function remove(node: OutlineTreeNode) {
    if (node.children.length && !window.confirm(text(
      `删除此章节及其 ${node.end - node.index - 1} 个下级章节？此操作还会移除这些章节的论文分配。`,
      `Delete this section and its ${node.end - node.index - 1} nested sections? Their paper assignments will also be removed.`,
    ))) return;
    commit({ ...draft, sections: [...draft.sections.slice(0, node.index), ...draft.sections.slice(node.end)] });
  }

  function renderNode(node: OutlineTreeNode, siblings: OutlineTreeNode[]) {
    const { index } = node;
    const section = draft.sections[index];
    const filterKey = section.sectionId || String(index);
    const filter = (paperFilters[filterKey] || "").toLowerCase();
    const visible = papers.filter((paper) => `${paperText(paper)} ${paperLabels.get(paper.paper_id) || ""}`.includes(filter));
    const siblingIndex = siblings.indexOf(node);
    const parent = section.headingLevel && section.headingLevel > 2
      ? [...draft.sections.slice(0, index)].reverse().find((candidate) => (candidate.headingLevel || 2) === section.headingLevel! - 1)
      : null;
    return <article className="outline-builder-card" key={`outline-section-${index}-${section.sectionId || "new"}`}>
      <div className="outline-builder-card-head"><div className="outline-builder-card-title"><strong>{node.number} {section.title || text("未命名章节", "Untitled section")}</strong><small>{node.children.length ? text("组织标题，不单独写正文", "Organizing heading · no separate prose") : text("正文小节", "Writing section")}</small></div><div>
        <button type="button" className="button button-quiet" aria-label={text(`上移 ${node.number}`, `Move ${node.number} up`)} disabled={siblingIndex === 0} onClick={() => move(index, -1)}>↑</button>
        <button type="button" className="button button-quiet" aria-label={text(`下移 ${node.number}`, `Move ${node.number} down`)} disabled={siblingIndex === siblings.length - 1} onClick={() => move(index, 1)}>↓</button>
        {section.headingLevel !== 6 ? <button type="button" className="button button-quiet" onClick={() => addSection(node)}>{text("添加下级", "Add child")}</button> : null}
        <button type="button" className="button button-quiet danger" onClick={() => remove(node)}>{text("删除", "Delete")}</button>
      </div></div>
      <label className="outline-builder-field"><span>{text("章节标题", "Section title")}</span><input value={section.title} onChange={(event) => updateSection(index, { title: event.target.value })} placeholder={text("例如：芳香族底物的反应范围", "e.g. Scope of aromatic substrates")} /></label>
      <div className="outline-builder-meta"><span>{parent ? text(`归属：${parent.title || "未命名上级"}`, `Under: ${parent.title || "untitled parent"}`) : text("主章节", "Main section")}</span><label>{text("层级", "Level")} <select aria-label={text(`${node.number} 的章节层级`, `Heading level for ${node.number}`)} value={section.headingLevel || 2} onChange={(event) => { const sections = changeOutlineLevel(draft.sections, index, Number(event.target.value)); if (sections) commit({ ...draft, sections }); }}>
        {[2, 3, 4, 5, 6].map((level) => {
          const targetParent = level > 2 ? [...draft.sections.slice(0, index)].reverse().find((candidate) => (candidate.headingLevel || 2) === level - 1) : null;
          return <option key={level} value={level} disabled={!changeOutlineLevel(draft.sections, index, level)}>{level === 2 ? text("一级章节", "Main section") : text(`${level - 1} 级小节 · 归属 ${targetParent?.title || "未找到上级"}`, `Level ${level - 1} · under ${targetParent?.title || "no parent"}`)}</option>;
        })}
      </select></label></div>
      <details className="outline-builder-details"><summary>{text("写作目标、论文与补充要求", "Writing goal, papers and notes")}{section.paperIds.length ? text(` · ${section.paperIds.length} 篇论文`, ` · ${section.paperIds.length} papers`) : ""}</summary>
        <label className="outline-builder-field"><span>{text("本节要回答什么问题", "What should this section answer?")}</span><textarea value={section.purpose} onChange={(event) => updateSection(index, { purpose: event.target.value })} placeholder={text("说明本节比较哪些工作、解决什么问题。", "Describe the papers and question this section should compare.")} /></label>
        {node.children.length && section.paperIds.length ? <p className="outline-context-note">{text("本章是组织标题；已分配论文将在规划时作为下级小节的支持文献。", "This is an organizing heading; assigned papers will support its writing subsections during planning.")}</p> : null}
        {section.contextPaperIds?.length ? <p className="outline-context-note">{text(`系统已将 ${section.contextPaperIds.length} 篇综述或观点文献作为背景证据，不计入正文主分类。`, `${section.contextPaperIds.length} review or perspective paper(s) are retained as contextual evidence rather than primary body evidence.`)}</p> : null}
        <details className="outline-paper-picker"><summary>{text(`选择论文（已选 ${section.paperIds.length} 篇）`, `Select papers (${section.paperIds.length} selected)`)}</summary><div className="outline-paper-picker-body"><input type="search" value={paperFilters[filterKey] || ""} onChange={(event) => setPaperFilters((current) => ({ ...current, [filterKey]: event.target.value }))} placeholder={text("按短序号或标题筛选", "Filter by short number or title")} /><div className="outline-paper-options">{visible.map((paper) => <label key={paper.paper_id} title={text(`内部论文 ID：${paper.paper_id}`, `Internal paper ID: ${paper.paper_id}`)}><input type="checkbox" checked={section.paperIds.includes(paper.paper_id)} onChange={(event) => updateSection(index, { paperIds: event.target.checked ? unique([...section.paperIds, paper.paper_id]) : section.paperIds.filter((paperId) => paperId !== paper.paper_id) })} /><span><strong>{paperLabels.get(paper.paper_id) || paper.paper_id}</strong> · {typeof paper.title === "string" ? paper.title : JSON.stringify(paper.title || "")}</span></label>)}</div></div></details>
        <label className="outline-builder-field"><span>{text("补充要求（可选）", "Additional instructions (optional)")}</span><textarea value={section.notes} onChange={(event) => updateSection(index, { notes: event.target.value })} placeholder={text("例如比较规则、机理重点、图表计划或段落衔接。", "Optional comparison rules, mechanism focus, figures, or transitions.")} /></label>
      </details>
      {node.children.length ? <div className="outline-builder-children">{node.children.map((child) => renderNode(child, node.children))}</div> : null}
    </article>;
  }

  if (mode === "markdown") {
    return (
      <div className="outline-builder-editor">
        <div className="outline-mode-switch"><button type="button" onClick={() => setMode("visual")}>{text("模块填写", "Module editor")}</button><button type="button" className="active" onClick={() => setMode("markdown")}>{text("代码填写", "Code editor")}</button></div>
        <textarea className="outline-editor" value={value} onChange={(event) => onChange(event.target.value)} spellCheck={false} />
        <p className="muted">{text("使用 Markdown 编辑大纲；## 是主章节，### 及更深层级为所属小节。原文会保持到您主动修改。", "Edit the outline in Markdown. ## marks a main section, ### and deeper levels mark nested subsections. The source stays unchanged until you edit it.")}</p>
      </div>
    );
  }

  return (
    <div className="outline-builder-editor">
      <div className="outline-mode-switch"><button type="button" className="active" onClick={() => setMode("visual")}>{text("模块填写", "Module editor")}</button><button type="button" onClick={() => setMode("markdown")}>{text("代码填写", "Code editor")}</button></div>
      <div className="outline-builder-toolbar"><div><strong>{text("按章节填写，直接查看上下级关系", "Fill sections while seeing their hierarchy")}</strong><p>{text("主章节下可添加小节；有下级的章节仅作组织标题。论文可稍后统一推荐并调整。", "Add subsections under a main section. Headings with children organize the outline; papers can be recommended and adjusted later.")}</p></div><div><button className="button button-secondary" type="button" disabled={!moduleEditable} onClick={addStarterSections}>{text("加入引言和结论", "Add introduction and conclusion")}</button><button className="button button-primary" type="button" disabled={!moduleEditable} onClick={() => addSection()}>{text("添加主章节", "Add main section")}</button></div></div>
      <div className={validation.ready ? "outline-builder-validation ready" : "outline-builder-validation"}>{validation.sectionCount ? text(`${validation.sectionCount} 个章节 · ${validation.missingTitles.length} 个缺少标题`, `${validation.sectionCount} sections · ${validation.missingTitles.length} missing titles`) : text("还没有章节，请添加章节或使用引言/结论模板。", "No sections yet. Add a section or use the introduction/conclusion starter.")}</div>
      {!moduleEditable ? <p className="message message-warning">{text("当前代码包含不连续层级或模块无法可靠解析的内容；原文已保留。请在“代码填写”中调整。", "The source has skipped heading levels or syntax the module editor cannot safely parse. It is preserved; edit it in Code editor.")}</p> : null}
      {draft.sections.length && moduleEditable ? <div className="outline-builder-list">{tree.roots.map((node) => renderNode(node, tree.roots))}</div> : !draft.sections.length && moduleEditable ? <div className="outline-builder-empty"><strong>{text("从一个空白大纲开始", "Start with a blank outline")}</strong><p>{text("添加主章节，再在章内添加小节。", "Add a main section, then add subsections within it.")}</p><button className="button button-primary" type="button" onClick={() => addSection()}>{text("添加第一个章节", "Add first section")}</button></div> : null}
    </div>
  );
}
