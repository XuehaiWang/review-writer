export type HardGateFinding = {
  paragraph_id: string;
  rule?: string;
  severity?: string;
  diagnosis?: string;
  route?: string;
};

export type HardGateDetail = {
  gate_id: string;
  findings: HardGateFinding[];
};

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function rows(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function findingRows(
  value: unknown,
  { requireBlockingSeverity = false }: { requireBlockingSeverity?: boolean } = {},
): HardGateFinding[] {
  const findings: HardGateFinding[] = [];
  const seen = new Set<string>();
  for (const item of rows(value)) {
    const paragraphId = String(item.paragraph_id || "").trim();
    const severity = String(item.severity || "").trim();
    if (!paragraphId || (requireBlockingSeverity && !["critical", "major"].includes(severity))) continue;
    const finding: HardGateFinding = {
      paragraph_id: paragraphId,
      rule: String(item.rule || "").trim() || undefined,
      severity: severity || undefined,
      diagnosis: String(item.diagnosis || item.message || "").trim() || undefined,
      route: String(item.route || "").trim() || undefined,
    };
    const identity = [finding.paragraph_id, finding.rule, finding.diagnosis].join("\u0000");
    if (seen.has(identity)) continue;
    seen.add(identity);
    findings.push(finding);
  }
  return findings;
}

function paragraphGateFindings(quality: JsonRecord): HardGateFinding[] {
  const preflight = isRecord(quality.preflight) ? quality.preflight : {};
  const exact = findingRows(preflight.paragraph_findings, { requireBlockingSeverity: true });
  if (exact.length) return exact;

  // Older imported reports may not contain the preflight block.  Fall back to
  // the scored paragraph collections, but still show only blocking severities.
  for (const candidate of [
    quality.blocking_paragraph_failures,
    quality.paragraph_failures,
    quality.issues,
  ]) {
    const fallback = findingRows(candidate, { requireBlockingSeverity: true });
    if (fallback.length) return fallback;
  }
  return [];
}

export function hardGateDetails(qualityValue: unknown): HardGateDetail[] {
  if (!isRecord(qualityValue)) return [];
  const failures = Array.isArray(qualityValue.hard_gate_failures)
    ? qualityValue.hard_gate_failures.map((value) => String(value || "").trim()).filter(Boolean)
    : [];
  return failures.map((gateId) => ({
    gate_id: gateId,
    findings: gateId === "paragraph_readability_or_source_failures"
      ? paragraphGateFindings(qualityValue)
      : [],
  }));
}
