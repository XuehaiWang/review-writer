export type MatrixRecommendationRow = {
  paper_id?: string;
  role?: string;
  score?: number;
  raw_score?: number;
  recommendation_status?: string;
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

type CandidateStatus = "recommended" | "review" | "excluded";

type Candidate = {
  paperId: string;
  status: CandidateStatus;
  firstSeen: number;
};

// "excluded" wins over a recommendation elsewhere in the results: a paper the
// author explicitly excluded must never be offered as a recommended pick.
const STATUS_PRIORITY: Record<CandidateStatus, number> = {
  excluded: 2,
  recommended: 1,
  review: 0,
};

function paperId(row: MatrixRecommendationRow): string {
  return String(row.paper_id || "").trim();
}

function score(row: MatrixRecommendationRow): number {
  const value = Number(row.score ?? row.raw_score ?? 0);
  return Number.isFinite(value) ? value : 0;
}

function statusFor(row: MatrixRecommendationRow): CandidateStatus {
  if (row.role === "excluded" || score(row) <= 0) return "excluded";
  if (row.recommendation_status === "recommended") return "recommended";
  return "review";
}

/**
 * Produces a deterministic, reviewable Matrix recommendation.
 *
 * The screening step is a plain include/exclude decision. The fine-grained
 * candidate roles (core/supporting/background/uncertain) never reach the
 * Matrix, so the recommendation is derived from retrieval evidence: the
 * server-supplied ``recommendation_status`` plus the author's explicit
 * exclusion and a positive score. Query groups explain retrieval coverage only
 * and therefore never cause a background paper to be selected.
 */
export function buildMatrixRecommendation(groups: MatrixRecommendationGroup[]): MatrixRecommendation {
  const activeGroups = groups.filter((group) => group.keep !== false);
  const candidates = new Map<string, Candidate>();
  let firstSeen = 0;

  for (const group of activeGroups) {
    for (const row of group.local_results || []) {
      const id = paperId(row);
      if (!id) continue;
      const status = statusFor(row);
      const existing = candidates.get(id);
      if (existing) {
        if (STATUS_PRIORITY[status] > STATUS_PRIORITY[existing.status]) {
          existing.status = status;
        }
      } else {
        candidates.set(id, { paperId: id, status, firstSeen: firstSeen++ });
      }
    }
  }

  const recommendedIds = new Set<string>();
  const reviewIds = new Set<string>();
  const excludedIds = new Set<string>();
  for (const candidate of candidates.values()) {
    if (candidate.status === "recommended") recommendedIds.add(candidate.paperId);
    else if (candidate.status === "excluded") excludedIds.add(candidate.paperId);
    else reviewIds.add(candidate.paperId);
  }

  return { recommendedIds, reviewIds, excludedIds };
}
