export type MetadataRecord = Record<string, unknown>;

export type BibliographicField = "title" | "authors" | "year" | "journal" | "doi" | "abstract";

export const STRUCTURED_TAG_KEYS = [
  "product",
  "substrate",
  "catalyst_or_method",
  "organometallic_partner",
  "ligand_or_chiral_source",
  "leaving_group",
  "reaction_type",
  "document_scope",
] as const;

export type StructuredTagKey = typeof STRUCTURED_TAG_KEYS[number];

export function isMetadataObject(value: unknown): value is MetadataRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function cloneMetadata(metadata: MetadataRecord): MetadataRecord {
  return structuredClone(metadata);
}

export function metadataFieldValue(metadata: MetadataRecord, key: string): unknown {
  const field = metadata[key];
  return isMetadataObject(field) && Object.prototype.hasOwnProperty.call(field, "value")
    ? field.value
    : field;
}

export function metadataFieldConfidence(metadata: MetadataRecord, key: string): number | null {
  const field = metadata[key];
  if (!isMetadataObject(field)) return null;
  const confidence = Number(field.confidence);
  return Number.isFinite(confidence) ? confidence : null;
}

export function metadataFieldIsHumanChecked(metadata: MetadataRecord, key: string): boolean {
  const field = metadata[key];
  return isMetadataObject(field) && field.human_checked === true;
}

export function updateBibliographicField(
  metadata: MetadataRecord,
  key: BibliographicField,
  value: unknown,
): MetadataRecord {
  const current = metadata[key];
  const field = isMetadataObject(current) && Object.prototype.hasOwnProperty.call(current, "value")
    ? current
    : {};
  return {
    ...metadata,
    [key]: {
      ...field,
      value,
      source: "human_review",
      source_page: undefined,
      source_block_index: undefined,
      confidence: 1,
      human_checked: true,
    },
  };
}

function structuredTagField(metadata: MetadataRecord): MetadataRecord {
  const field = metadata.structured_tags;
  return isMetadataObject(field) && Object.prototype.hasOwnProperty.call(field, "value")
    ? field
    : { value: {} };
}

export function structuredTagValue(metadata: MetadataRecord, key: StructuredTagKey): string {
  const value = structuredTagField(metadata).value;
  if (!isMetadataObject(value)) return "";
  return String(value[key] || "");
}

export function updateStructuredTag(
  metadata: MetadataRecord,
  key: StructuredTagKey,
  value: string,
): MetadataRecord {
  const field = structuredTagField(metadata);
  const currentValues = isMetadataObject(field.value) ? field.value : {};
  const changed = String(currentValues[key] || "") !== value;
  return {
    ...metadata,
    structured_tags: {
      ...field,
      ...(changed ? {
        source: "human_edit_unverified",
        confidence: 0,
        human_checked: false,
      } : {}),
      value: {
        ...currentValues,
        [key]: value,
      },
    },
  };
}

export function setStructuredTagsVerified(metadata: MetadataRecord, verified: boolean): MetadataRecord {
  const field = structuredTagField(metadata);
  return {
    ...metadata,
    structured_tags: {
      ...field,
      source: verified ? "human_review" : field.source,
      confidence: verified ? 1 : field.confidence,
      human_checked: verified,
    },
  };
}

export function markMetadataReviewed(metadata: MetadataRecord, reviewedAt: string): MetadataRecord {
  const current = isMetadataObject(metadata.human_review) ? metadata.human_review : {};
  return {
    ...metadata,
    human_review: {
      ...current,
      status: "reviewed",
      reviewed_at: reviewedAt,
      reviewer: current.reviewer || "human",
    },
  };
}

export function authorsFromInput(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((author) => author.trim())
    .filter(Boolean);
}

export function metadataTextForEditing(value: unknown): string {
  return String(value ?? "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, "\"")
    .replace(/&#39;/gi, "'");
}

export function metadataForEditing(metadata: MetadataRecord): MetadataRecord {
  const editable = cloneMetadata(metadata);
  const field = structuredTagField(editable);
  if (!isMetadataObject(field.value)) return editable;
  const values = { ...field.value };
  for (const key of STRUCTURED_TAG_KEYS) {
    if (String(values[key] || "").trim().toLocaleLowerCase() === "not specified") {
      values[key] = "";
    }
  }
  return { ...editable, structured_tags: { ...field, value: values } };
}

export function metadataForSave(metadata: MetadataRecord): MetadataRecord {
  let normalized = metadata;
  const authors = metadataFieldValue(normalized, "authors");
  if (typeof authors === "string") {
    const field = normalized.authors;
    normalized = {
      ...normalized,
      authors: isMetadataObject(field) && Object.prototype.hasOwnProperty.call(field, "value")
        ? { ...field, value: authorsFromInput(authors) }
        : authorsFromInput(authors),
    };
  }
  const tagField = structuredTagField(normalized);
  if (isMetadataObject(tagField.value)) {
    const tagValues = { ...tagField.value };
    for (const key of STRUCTURED_TAG_KEYS) {
      tagValues[key] = String(tagValues[key] || "").trim() || "not specified";
    }
    normalized = { ...normalized, structured_tags: { ...tagField, value: tagValues } };
  }
  return normalized;
}

export function metadataValidationError(metadata: MetadataRecord, currentYear = new Date().getFullYear()): "title" | "year" | null {
  if (!String(metadataFieldValue(metadata, "title") || "").trim()) return "title";
  const year = metadataFieldValue(metadata, "year");
  if (year !== null && year !== undefined && String(year).trim()) {
    const numericYear = Number(year);
    if (!Number.isInteger(numericYear) || numericYear < 1000 || numericYear > currentYear + 1) return "year";
  }
  return null;
}
