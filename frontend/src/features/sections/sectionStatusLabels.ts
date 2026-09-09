export type UiText = (zh: string, en: string) => string;

export function sectionReadinessLabel(
  status: string | undefined,
  text: UiText,
) {
  const labels: Record<string, string> = {
    pending_evidence: text("待补充 · 可继续编辑", "Evidence pending · editing available"),
    limited_evidence: text("证据有限 · 可继续编辑", "Limited evidence · editing available"),
    scientific_complete: text("科学就绪", "Scientifically ready"),
    needs_evidence_repair: text("需补证据", "Needs evidence repair"),
    needs_structure_repair: text("需补结构", "Needs structure repair"),
    evidence_safe_but_shallow: text("正文可用，未达建议字数", "Usable prose; below target length"),
    provider_fallback: text("服务降级保底", "Provider fallback"),
    failed: text("未就绪", "Not ready"),
  };
  return status ? labels[status] || status : "";
}

export function sectionGenerationLabel(
  mode: string | undefined,
  text: UiText,
) {
  if (mode === "pending_evidence") return text("待补充", "Evidence pending");
  if (mode === "limited_evidence") return text("证据有限", "Limited evidence");
  if (mode === "safe_evidence_fallback") return text("安全保底", "Safe fallback");
  if (mode === "evidence_repaired") return text("自动修复", "Repaired");
  return text("标准生成", "Standard");
}
