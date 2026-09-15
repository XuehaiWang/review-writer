import type { Job } from "../../../api/types";
import { buildPaperDisplayLabels } from "../../../utils/paperLabels";

export type DiscoveryRow = Record<string, unknown> & {
  paper_id?: string;
  display_label?: string;
  candidate_id?: string;
  title?: string;
  authors?: string[];
  year?: number | string;
  journal?: string;
  score?: number;
  role?: string;
  keep?: boolean;
  selected_for_matrix?: boolean;
  source?: string;
  landing_url?: string;
  pdf_url?: string;
  access_status?: "open_access_downloadable" | "institution_required" | "metadata_only" | "downloaded_to_library" | "access_unknown";
  recommendation_status?: "recommended" | "review" | "background" | "excluded";
  retrieval_channels?: string[];
  matched_partitions?: string[];
  lexical_partition_candidates?: string[];
  semantic_partition_candidates?: string[];
  classification_status?: "evidence_backed_screening" | "screening_evidence_supported" | "pending_evidence" | "deferred_to_matrix" | "out_of_scope";
  semantic_index_status?: string;
  screening_chunks?: Array<{ chunk_id?: string; page_start?: number; section_path?: string[]; excerpt?: string; channel?: string }>;
};

export type DiscoveryGroup = {
  keyword: string;
  category?: string;
  system_group?: string;
  classification_status?: "evidence_backed_screening" | "pending_evidence" | "deferred_to_matrix" | "out_of_scope";
  keep?: boolean;
  local_results?: DiscoveryRow[];
  web_results?: DiscoveryRow[];
};

export type DiscoveryPayload = {
  project_id: string;
  artifact_id: string;
  revision: number;
  status?: string;
  has_published_matrix?: boolean;
  topic: string;
  keywords?: string;
  query_plan_source?: string;
  query_plan?: {
    planner?: string;
    planner_notice?: string;
    planner_notice_code?: string;
    group_by?: string[];
    semantic_queries?: Array<{
      query_id?: string;
      kind?: string;
      label?: string;
      axis_id?: string;
      partition_id?: string;
    }>;
  };
  results: DiscoveryGroup[];
  statistics?: {
    candidate_count?: number;
    keyword_hit_count?: number;
    selected_count?: number;
    keyword_group_count?: number;
    external_candidate_count?: number;
    category_count?: number;
    unclassified_keyword_group_count?: number;
  };
  coverage_mode?: "local_bounded" | "multi_source";
  coverage_decision?: "keep_local";
  coverage_confirmation_required?: boolean;
  capability_warnings?: string[];
  coverage_diagnostics?: {
    coverage_mode?: "local_bounded" | "multi_source";
    candidate_paper_count?: number;
    year_distribution?: Record<string, number>;
    year_unknown_count?: number;
    declared_year_from?: number | null;
    declared_year_to?: number | null;
    missing_years?: number[];
    empty_query_groups?: string[];
    requested_online_sources?: string[];
    online_search_suggested?: boolean;
    reason_codes?: string[];
  };
  search_record?: {
    requested_sources?: string[];
    enabled_sources?: string[];
    executed_sources?: string[];
    failed_sources?: string[];
    completion_state?: string;
    query_log?: Array<{ query_group?: string; query?: string; source_results?: unknown[] }>;
    initial_local_hit_count?: number;
    unique_local_candidate_count?: number;
    initial_external_hit_count?: number;
    unique_external_candidate_count?: number;
    selected_matrix_candidate_count?: number;
  };
  hybrid_retrieval?: {
    status?: string;
    semantic_status?: string;
    semantic_reason?: string;
    semantic_indexed_paper_count?: number;
    library_paper_count?: number;
    embedding_model?: string;
    embedding_dimension?: number;
    external_screening?: { status?: string; reason?: string };
  };
};

export type DiscoveryJobState = { active_job: Job | null; latest_job: Job | null };
export type CandidateFilter = "all" | "recommended" | "review" | "selected" | "metadata_rules" | "fulltext_lexical" | "semantic" | "online";
export type TextSelector = (zh: string, en: string) => string;
export type UnifiedDiscoveryRow = { row: DiscoveryRow; kind: "local" | "web" };

export const CANDIDATE_FILTERS: CandidateFilter[] = [
  "all", "recommended", "review", "selected", "metadata_rules", "fulltext_lexical", "semantic", "online",
];

export function publicPlannerNotice(plan: DiscoveryPayload["query_plan"], text: TextSelector): string {
  if (plan?.planner_notice_code === "concept_expansion_unavailable") {
    return text(
      "缩写补充暂不可用，已保留原始主题词继续检索；这不影响后续依据论文内容进行分析。",
      "Optional abbreviation expansion was unavailable. Retrieval continued with the original terms; subsequent evidence-based analysis is unchanged.",
    );
  }
  const raw = String(plan?.planner_notice || "");
  const insufficient = plan?.planner_notice_code === "insufficient_credit"
    || /INSUFFICIENT_CREDIT|HTTP\s*402|余额不足/i.test(raw);
  return insufficient
    ? text(
      "余额不足，智能查询规划未运行。本次检索已自动使用确定性查询规划；请在“API 设置”中查看余额，或联系管理员添加额度。",
      "Your balance is insufficient for intelligent query planning. Deterministic planning was used automatically; review your balance in API Settings or contact an administrator for credit.",
    )
    : text(
      "智能查询规划暂不可用，本次检索已自动使用确定性查询规划。",
      "Intelligent query planning was temporarily unavailable, so deterministic planning was used automatically.",
    );
}

export function groupLabel(group: DiscoveryGroup | undefined, text: TextSelector): string {
  if (group?.system_group === "__topic_candidates_pending_evidence__") {
    return text("混合召回的 Topic 候选", "Topic candidates from hybrid retrieval");
  }
  return group?.keyword || text("结果", "Results");
}

export function queryGroupSourceLabel(group: DiscoveryGroup, text: TextSelector): string {
  if (group.system_group === "__topic_candidates_pending_evidence__") {
    return text("混合召回补充", "Hybrid retrieval supplement");
  }
  const channels = new Set((group.local_results || []).flatMap((row) => row.retrieval_channels || []));
  const labels: string[] = [];
  if (channels.has("metadata_rules") || !channels.size) labels.push(text("题录/规则", "Metadata/rules"));
  if (channels.has("fulltext_lexical")) labels.push(text("全文", "Full text"));
  if (channels.has("semantic")) labels.push(text("语义", "Semantic"));
  if ((group.web_results || []).length) labels.push(text("联网", "Online"));
  return labels.join(" · ") || text("查询规划组", "Planned query group");
}

export function selectedForMatrix(row: DiscoveryRow): boolean {
  return row.selected_for_matrix === true && row.role !== "excluded";
}

export function retrievalChannelLabel(channel: string, text: TextSelector): string {
  const labels: Record<string, [string, string]> = {
    metadata_rules: ["精确命中", "Exact match"],
    fulltext_lexical: ["全文命中", "Full-text match"],
    semantic: ["语义补充", "Semantic supplement"],
    title_abstract_lexical: ["标题摘要", "Title / abstract"],
    title_abstract_semantic: ["外部语义", "External semantic"],
  };
  const value = labels[channel] || [channel, channel];
  return text(value[0], value[1]);
}

export function externalActionLabel(status: DiscoveryRow["access_status"], text: TextSelector): string {
  if (status === "open_access_downloadable") return text("下载并解析", "Download and parse");
  if (status === "institution_required") return text("需要机构权限", "Institution access");
  if (status === "metadata_only") return text("仅有题录", "Metadata only");
  return text("查看来源", "View source");
}

export function localCandidateId(row: DiscoveryRow): string {
  return String(row.paper_id || "").trim();
}

export function externalCandidateId(row: DiscoveryRow): string {
  return String(row.candidate_id || row.doi || row.landing_url || `${row.title || ""}|${row.year || ""}`).trim();
}

function mergeRows(left: DiscoveryRow, right: DiscoveryRow): DiscoveryRow {
  const leftScore = Number(left.hybrid_score || left.score || left.raw_score || 0);
  const rightScore = Number(right.hybrid_score || right.score || right.raw_score || 0);
  const preferred = rightScore > leftScore ? right : left;
  return {
    ...left,
    ...right,
    ...preferred,
    selected_for_matrix: selectedForMatrix(left) || selectedForMatrix(right),
    retrieval_channels: [...new Set([...(left.retrieval_channels || []), ...(right.retrieval_channels || [])])],
    matched_partitions: [...new Set([...(left.matched_partitions || []), ...(right.matched_partitions || [])])],
    lexical_partition_candidates: [...new Set([...(left.lexical_partition_candidates || []), ...(right.lexical_partition_candidates || [])])],
    semantic_partition_candidates: [...new Set([...(left.semantic_partition_candidates || []), ...(right.semantic_partition_candidates || [])])],
    screening_chunks: [...new Map(
      [...(left.screening_chunks || []), ...(right.screening_chunks || [])]
        .map((chunk) => [String(chunk.chunk_id || `${chunk.page_start || ""}|${chunk.excerpt || ""}`), chunk]),
    ).values()].slice(0, 3),
  };
}

export function unifyDiscoveryRows(groups: DiscoveryGroup[]): UnifiedDiscoveryRow[] {
  const local = new Map<string, DiscoveryRow>();
  const external = new Map<string, DiscoveryRow>();
  for (const group of groups) {
    if (group.keep === false) continue;
    for (const row of group.local_results || []) {
      const id = localCandidateId(row);
      if (id) local.set(id, local.has(id) ? mergeRows(local.get(id)!, row) : { ...row });
    }
    for (const row of group.web_results || []) {
      const id = externalCandidateId(row);
      if (id) external.set(id, external.has(id) ? mergeRows(external.get(id)!, row) : { ...row });
    }
  }
  return [
    ...[...local.values()].map((row) => ({ row, kind: "local" as const })),
    ...[...external.values()].map((row) => ({ row, kind: "web" as const })),
  ].sort((left, right) => {
    const leftScore = Number(left.row.hybrid_score || left.row.score || left.row.raw_score || 0);
    const rightScore = Number(right.row.hybrid_score || right.row.score || right.row.raw_score || 0);
    return rightScore - leftScore || String(left.row.title || "").localeCompare(String(right.row.title || ""));
  });
}

export function candidateMatchesFilter(
  entry: UnifiedDiscoveryRow,
  filter: CandidateFilter,
  recommendedIds: Set<string>,
  reviewIds: Set<string>,
): boolean {
  const { row, kind } = entry;
  const id = localCandidateId(row);
  if (filter === "all") return true;
  if (filter === "online") return kind === "web";
  if (filter === "selected") return kind === "local" && selectedForMatrix(row);
  if (filter === "recommended") return kind === "local" && recommendedIds.has(id);
  if (filter === "review") return kind === "local" && reviewIds.has(id);
  return kind === "local" && (row.retrieval_channels || []).includes(filter);
}

export function buildDiscoveryPaperLabels(groups: DiscoveryGroup[]): Map<string, string> {
  const ranked = new Map<string, { paper_id: string; display_label?: string; score: number; order: number }>();
  const remaining: Array<{ paper_id: string; display_label?: string }> = [];
  const seenRemaining = new Set<string>();
  let order = 0;
  for (const group of groups) {
    if (group.keep === false) continue;
    for (const row of group.local_results || []) {
      const paperId = String(row.paper_id || "").trim();
      if (!paperId) continue;
      if (!seenRemaining.has(paperId)) {
        seenRemaining.add(paperId);
        remaining.push({ paper_id: paperId, display_label: row.display_label });
      }
      if (selectedForMatrix(row)) {
        const score = Number(row.score || row.raw_score || 0);
        const previous = ranked.get(paperId);
        if (!previous || score > previous.score) ranked.set(paperId, { paper_id: paperId, display_label: row.display_label, score, order });
      }
      order += 1;
    }
  }
  const selected = [...ranked.values()].sort((left, right) => right.score - left.score || left.order - right.order);
  const selectedIds = new Set(selected.map((row) => row.paper_id));
  return buildPaperDisplayLabels([...selected, ...remaining.filter((row) => !selectedIds.has(row.paper_id))]);
}

export function orderedQueryGroups(groups: DiscoveryGroup[]): DiscoveryGroup[] {
  return [
    ...groups.filter((group) => group.system_group !== "__topic_candidates_pending_evidence__"),
    ...groups.filter((group) => group.system_group === "__topic_candidates_pending_evidence__"),
  ];
}
