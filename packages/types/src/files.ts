/** Mirrors apps/backend's file endpoints
 * (app/presentation/api/v1/schemas.py's File*Response classes). */
export interface FileSummary {
  id: string;
  name: string;
  path: string;
  mime_type: string | null;
  size_bytes: number | null;
  is_shared: boolean;
  owner_email: string | null;
  provider_modified_at: string | null;
  /** Only ever populated by the Trash listing — whether a completed
   * archive already backs this file, the eligibility requirement for
   * permanent deletion. `null`/absent everywhere else. */
  is_archived?: boolean | null;
}

export interface FileListResponse {
  items: FileSummary[];
  total: number;
}

/** Backs the Files browser's "Move to folder" picker.
 * `provider_file_id` is Drive's own id — the value a move plan's
 * `new_parent_id` must carry, not this platform's `id`. */
export interface FolderSummary {
  id: string;
  provider_file_id: string;
  name: string;
  path: string;
}

export interface FileMetadata {
  normalized_extension: string | null;
  mime_type_validated: boolean;
  mime_mismatch_reason: string | null;
  naming_pattern: string | null;
  version_label: string | null;
  owner_summary: string | null;
  sharing_summary: string | null;
  duplicate_group_key: string | null;
  language: string | null;
  enriched_at: string;
}

export interface FileClassification {
  document_type: string;
  confidence: number;
  method: string;
  classified_at: string;
}

export type FileExtractionStatus = "success" | "failed" | "unsupported" | "skipped_too_large";

export interface FileExtractionInfo {
  status: FileExtractionStatus;
  extractor_name: string | null;
  char_count: number | null;
  error: string | null;
  extracted_at: string;
}

export type FileIntelligenceStatus = "success" | "failed" | "unsupported";

export interface FileIntelligenceEntity {
  type: string;
  value: string;
  confidence: number | null;
}

/** Unlike `FileExtractionInfo`'s deliberate exclusion of raw extracted
 * text, `summary` here is meant to render directly — it's a distilled,
 * human-facing summary, not raw extracted text. */
export interface FileIntelligence {
  status: FileIntelligenceStatus;
  document_type: string | null;
  summary: string | null;
  entities: FileIntelligenceEntity[];
  structured_metadata: Record<string, unknown>;
  topics: string[];
  confidence: number | null;
  provider: string;
  model_name: string;
  error: string | null;
  processed_at: string;
}

export interface KnowledgeAttribute {
  attribute_type: string;
  value: string;
  confidence: number;
  source: string;
}

export type RelationshipType =
  | "sequential_version"
  | "duplicate_candidate"
  | "shared_ownership"
  | "near_duplicate";

export interface RelatedFile {
  file_id: string;
  name: string;
  path: string;
  relationship_type: RelationshipType;
  confidence: number;
  metadata: Record<string, unknown>;
}

/** One file's link to a discovered `OrganizationEntity` (Project/Client/
 * Campaign) — see organization.ts for the entity itself. */
export interface FileEntityLink {
  entity_id: string;
  entity_type: string;
  entity_name: string;
  confidence: number;
  evidence: Array<{ type: string; description: string }>;
  is_user_confirmed: boolean;
  source: "ai_inferred" | "user_assigned";
}

export type FileLifecycleState =
  | "keep"
  | "active"
  | "reference"
  | "archive_candidate"
  | "duplicate_candidate"
  | "obsolete_candidate"
  | "review_required"
  | "unknown";

export interface FileLifecycle {
  state: FileLifecycleState;
  confidence: number;
  evidence: string[];
  analyzed_at: string;
}

export interface FileDetail {
  id: string;
  /** The connection the file lives in. */
  connector_id: string | null;
  provider: string | null;
  name: string;
  path: string;
  mime_type: string | null;
  size_bytes: number | null;
  is_shared: boolean;
  owner_email: string | null;
  provider_modified_at: string | null;
  web_view_link: string | null;
  metadata: FileMetadata | null;
  classification: FileClassification | null;
  extraction: FileExtractionInfo | null;
  intelligence: FileIntelligence | null;
  knowledge_attributes: KnowledgeAttribute[];
  related_files: RelatedFile[];
  entity_links: FileEntityLink[];
  lifecycle: FileLifecycle | null;
}
