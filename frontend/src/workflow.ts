import type { MessageKey } from "./i18n/messages";

// Public IDs match review_writer_api.workflow_contracts.USER_STAGES.
export const workflowLinks: Array<{ id: string; href: string; label: MessageKey; stage: string; hintZh: string; hintEn: string }> = [
  { id: "library", href: "/library", label: "library", stage: "01", hintZh: "文献准备", hintEn: "Sources" },
  { id: "discovery", href: "/discovery", label: "discovery", stage: "02", hintZh: "文献筛选", hintEn: "Literature screening" },
  { id: "planning", href: "/planning?view=reading", label: "planning", stage: "03", hintZh: "文献与大纲", hintEn: "Papers & outline" },
  { id: "sections", href: "/sections", label: "sections", stage: "04", hintZh: "规划与正文", hintEn: "Plan & drafts" },
  { id: "images", href: "/images?tab=review", label: "images", stage: "05", hintZh: "选图重绘", hintEn: "Figures" },
  { id: "draft", href: "/draft", label: "draft", stage: "06", hintZh: "编辑优化", hintEn: "Revision" },
  { id: "final", href: "/final", label: "final", stage: "07", hintZh: "审计导出", hintEn: "Release" },
];

// Only for cached responses from earlier releases; new APIs expose public IDs.
export function workflowStage(id: string) {
  const legacy: Record<string, string> = { matrix: "planning", blueprint: "sections", "figure-review": "images", figures: "images" };
  return workflowLinks.find(stage => stage.id === (legacy[id] || id));
}

export function projectWorkflowPath(stageId: string, projectId: string) {
  const href = workflowStage(stageId)?.href || "/library";
  return `${href}${href.includes("?") ? "&" : "?"}project=${encodeURIComponent(projectId)}`;
}
