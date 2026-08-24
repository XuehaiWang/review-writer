export type MatrixRecommendationRow = {
  paper_id?: string;
  role?: string;
  score?: number;
  raw_score?: number;
};

export type MatrixRecommendationGroup = {
  keep?: boolean;
  local_results?: MatrixRecommendationRow[];
};

export type MatrixRecommendation = {
  recommendedIds: Set<string>;
  reviewIds: Set<string>;
  excludedIds: Set<string>;
};

type Candidate = {
  paperId: string;
  bestRole: string;
  bestScore: number;
  firstSeen: number;
};

const ROLE_PRIORITY: Record<string, number> = {
  core_candidate: 4,
  supporting_candidate: 3,
  background: 2,
  uncertain: 1,
  excluded: 0,
};

function paperId(row: MatrixRecommendationRow): string {
  return String(row.paper_id || "").trim();
}

function score(row: MatrixRecommendationRow): number {
  const value = Number(row.score ?? row.raw_score ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function strongerRole(left: string, right: string): string {
  return (ROLE_PRIORITY[right] ?? 1) > (ROLE_PRIORITY[left] ?? 1) ? right : left;
}

/**
 * Produces a deterministic, reviewable Matrix recommendation.
 *
 * Core/supporting candidates are recommended first. If an active query group
 * has no such representative, its strongest background paper is added for
 * coverage. Uncertain papers remain for human review and excluded/zero-score
 * papers are never selected automatically.
 */
export function buildMatrixRecommendation(groups: MatrixRecommendationGroup[]): MatrixRecommendation {
  const activeGroups = groups.filter((group) => group.keep !== false);
  const candidates = new Map<string, Candidate>();
  let firstSeen = 0;

  for (const group of activeGroups) {
    for (const row of group.local_results || []) {
      const id = paperId(row);
      if (!id) continue;
      const role = String(row.role || "uncertain");
      const existing = candidates.get(id);
      if (existing) {
        existing.bestRole = strongerRole(existing.bestRole, role);
        existing.bestScore = Math.max(existing.bestScore, score(row));
      } else {
        candidates.set(id, {
          paperId: id,
          bestRole: role,
          bestScore: score(row),
          firstSeen: firstSeen++,
        });
      }
    }
  }

  const recommendedIds = new Set(
    [...candidates.values()]
      .filter((candidate) => candidate.bestScore > 0 && ["core_candidate", "supporting_candidate"].includes(candidate.bestRole))
      .map((candidate) => candidate.paperId),
  );

  // Preserve query-facet coverage without admitting weak or uncertain papers.
  for (const group of activeGroups) {
    const groupIds = new Set((group.local_results || []).map(paperId).filter(Boolean));
    if ([...groupIds].some((id) => recommendedIds.has(id))) continue;
    const representative = [...groupIds]
      .map((id) => candidates.get(id))
      .filter((candidate): candidate is Candidate => Boolean(candidate))
      .filter((candidate) => candidate.bestRole === "background" && candidate.bestScore >= 0.15)
      .sort((left, right) => right.bestScore - left.bestScore || left.firstSeen - right.firstSeen)[0];
    if (representative) recommendedIds.add(representative.paperId);
  }

  const reviewIds = new Set<string>();
  const excludedIds = new Set<string>();
  for (const candidate of candidates.values()) {
    if (recommendedIds.has(candidate.paperId)) continue;
    if (candidate.bestRole === "excluded" || candidate.bestScore <= 0) {
      excludedIds.add(candidate.paperId);
    } else {
      reviewIds.add(candidate.paperId);
    }
  }

  return { recommendedIds, reviewIds, excludedIds };
}
