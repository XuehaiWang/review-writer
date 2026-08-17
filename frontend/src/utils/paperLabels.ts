export type PaperIdentity = { paper_id: string };

export function buildPaperDisplayLabels(papers: PaperIdentity[]): Map<string, string> {
  const labels = new Map<string, string>();
  const width = Math.max(3, String(papers.length).length);
  papers.forEach((paper, index) => {
    if (!labels.has(paper.paper_id)) labels.set(paper.paper_id, `P${String(index + 1).padStart(width, "0")}`);
  });
  return labels;
}

export function displayFigureLabel(
  figureId: string,
  paperId: string,
  paperLabels: Record<string, string>,
): string {
  const paperLabel = paperLabels[paperId];
  if (!paperLabel) return figureId;
  if (!figureId) return paperLabel;
  if (paperId && figureId.startsWith(paperId)) {
    return `${paperLabel}${figureId.slice(paperId.length)}`;
  }
  return figureId;
}

export function replacePaperIdsForDisplay(
  content: string | undefined,
  labels: Map<string, string> | Record<string, string>,
): string {
  let result = String(content || "");
  const entries = labels instanceof Map ? [...labels.entries()] : Object.entries(labels);
  for (const [paperId, displayLabel] of entries.sort((left, right) => right[0].length - left[0].length)) {
    if (!paperId || paperId === displayLabel) continue;
    result = result.split(paperId).join(displayLabel);
  }
  return result;
}
