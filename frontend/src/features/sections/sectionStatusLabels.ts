export type UiText = (zh: string, en: string) => string;

export function sectionReadinessLabel(
  status: string | undefined,
  text: UiText,
) {
  const labels: Record<string, string> = {
    scientific_complete: text("科学就绪", "Scientifically ready"),
    needs_evidence_repair: text("需补证据", "Needs evidence repair"),
    needs_structure_repair: text("需补结构", "Needs structure repair"),
    evidence_safe_but_shallow: text("证据安全但偏浅", "Evidence-safe but shallow"),
    provider_fallback: text("服务降级保底", "Provider fallback"),
    failed: text("未就绪", "Not ready"),
  };
  return status ? labels[status] || status : "";
}

export function sectionGenerationLabel(
  mode: string | undefined,
  text: UiText,
) {
  if (mode === "safe_evidence_fallback") return text("安全保底", "Safe fallback");
  if (mode === "evidence_repaired") return text("自动修复", "Repaired");
  return text("标准生成", "Standard");
}
