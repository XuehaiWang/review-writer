export type FactProcessing = {
  reading?: string;
  extraction?: string;
  verification?: string;
  source_recovery?: string;
};

export type FactExtractionProfile = {
  stop_reason?: string;
  reading_strategy?: string;
  reading_status?: string;
  reading_source_key?: string;
};

export type MatrixFact = {
  fact_id?: string;
  field_id?: string;
  value?: string;
  support_excerpt?: string;
  epistemic_status?: string;
  source_channel?: string;
  support_level?: string;
  review_status?: string;
  confidence?: number;
  evidence_ceiling?: string;
  assertion_ceiling?: string;
  validation_contract?: string;
  verification?: { status?: string; method?: string };
  revision_of_fact_id?: string;
  superseded_by_fact_id?: string;
  evidence_refs?: Array<{
    evidence_key?: string;
    source_file_id?: string;
    paper_id?: string;
    page_start?: number | null;
    page_end?: number | null;
    chunk_id?: string;
    support_excerpt?: string;
  }>;
};

export type PaperAnalysis = {
  research_question?: string;
  contribution?: string;
  topic_relation?: string;
  evidence_keys?: string[];
  fact_ids?: string[];
  usage?: string;
};

/** Server-derived per-paper fact checks for list rows that omit the records. */
export type FactsDigest = {
  has_pending_revision?: boolean;
  has_limited_fact?: boolean;
};

export type ReadingPaper = {
  scientific_facts?: unknown[];
  facts_digest?: FactsDigest;
  fact_enrichment?: {
    processing?: FactProcessing;
    fact_extraction_profile?: FactExtractionProfile;
    pending_revision_fact_ids?: string[];
  };
};

export function readingCompleted(paper: ReadingPaper): boolean {
  const enrichment = paper.fact_enrichment;
  if (enrichment?.processing?.reading) return enrichment.processing.reading === "completed";
  const profile = enrichment?.fact_extraction_profile;
  return profile?.reading_strategy === "review-oriented/1" && profile.reading_status === "completed";
}

export function hasPendingFactRevision(paper: ReadingPaper): boolean {
  // List rows carry only the server-derived digest; the record scan below is for
  // the detail row, which loads the fact records themselves.
  if (paper.facts_digest) return Boolean(paper.facts_digest.has_pending_revision);
  if (paper.fact_enrichment?.pending_revision_fact_ids?.length) return true;
  return Boolean(paper.scientific_facts?.some(item => {
    const fact = item as MatrixFact | null;
    return fact?.revision_of_fact_id && !fact.superseded_by_fact_id
      && !["supported", "uncertain", "rejected"].includes(fact.verification?.status || "");
  }));
}

/** Display groups do not grant evidence eligibility; that remains a server decision. */
export function factDisplayGroup(fact: MatrixFact, newReading = false): "verified" | "candidate" | "limited" | "historical" {
  const status = fact.verification?.status;
  if (fact.superseded_by_fact_id) return "historical";
  if (status === "supported" && !["context_only", "coverage_only", "neighbor_context"].includes(fact.support_level || "")) return "verified";
  if (!status && !fact.validation_contract && !newReading) return "historical";
  if (!status || status === "pending") return "candidate";
  return "limited";
}
