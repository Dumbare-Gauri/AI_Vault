/** Mirrors apps/backend's conversation endpoints
 * (app/presentation/api/v1/schemas.py's Conversation, Citation, AskRequest, and AskResponse classes). */
import type { RetrievalMethod } from "./search";

export interface AskRequest {
  question: string;
}

/** "tool" (ADR-024) — set on a citation/message produced by the AI Storage
 * Assistant's deterministic tool-routing path, distinct from `RetrievalMethod`
 * (search.ts), which describes only `SearchService`'s own metadata/semantic
 * retrieval and never includes "tool". */
export type ConversationRetrievalMethod = RetrievalMethod | "tool";

export interface Citation {
  id: string;
  file_id: string;
  snippet: string | null;
  confidence: number;
  retrieval_method: ConversationRetrievalMethod;
  /** Where in the file the answer came from, when it came from file content. */
  page_number: number | null;
  passage_index: number | null;
  file_name: string | null;
  file_size_bytes: number | null;
  file_mime_type: string | null;
}

export interface ConversationMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  retrieval_method: ConversationRetrievalMethod | null;
  provider: string | null;
  token_usage: number | null;
  tool_name: string | null;
  created_at: string;
  citations: Citation[];
  /** Structured parts of the answer, rendered as real interface. */
  blocks: AnswerBlock[];
}

/** A file as Ask Vault shows it — never an id the user has to read. */
export interface AnswerFile {
  id: string;
  name: string;
  path: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  modified_at: string | null;
  storage: string | null;
  connector_id: string | null;
  trashed: boolean;
  /** Duplicate groups only: the copy AI Vault recommends keeping. */
  keep?: boolean;
  /** Duplicate cleanup only: the kept file this copy is identical to. */
  duplicate_of?: AnswerFile;
}

export interface FileListBlock {
  type: "file_list";
  title: string;
  total: number;
  items: AnswerFile[];
  note: string | null;
}

export interface AnswerDuplicateGroup {
  id: string;
  /** Start of the content fingerprint every copy shares — the evidence. */
  fingerprint: string | null;
  size_bytes: number | null;
  file_count: number;
  recoverable_bytes: number | null;
  keep_reason: string | null;
  files: AnswerFile[];
}

export interface DuplicateGroupsBlock {
  type: "duplicate_groups";
  title: string;
  total_groups: number;
  recoverable_bytes: number;
  groups: AnswerDuplicateGroup[];
}

export interface StorageSummaryEntry {
  label: string;
  is_local: boolean;
  indexed_files: number | null;
  indexed_bytes: number | null;
  provider_used_bytes: number | null;
  provider_total_bytes: number | null;
  provider_trash_bytes: number | null;
  potential_savings_bytes: number | null;
  duplicate_recoverable_bytes: number | null;
  last_synced_at: string | null;
  operations: string[];
  breakdown: { category: string; bytes: number }[];
}

export interface StorageSummaryBlock {
  type: "storage_summary";
  storages: StorageSummaryEntry[];
}

export interface OrganizationPreview {
  root: string;
  folders: {
    name: string;
    reason: string | null;
    confidence: number | null;
    count: number;
    items: AnswerFile[];
  }[];
  unsure: AnswerFile[];
  unsure_count: number;
  folders_created: number;
  files_moved: number;
  files_renamed: number;
  files_deleted: number;
}

export interface ActionProposalBlock {
  type: "action_proposal";
  action_id: string;
  kind: string;
  title: string;
  description: string;
  risk: "low" | "medium" | "high";
  confirm_label: string;
  affected_count: number;
  items: AnswerFile[];
  preview: OrganizationPreview | null;
  status: "pending" | "confirmed" | "cancelled";
}

export interface ActionResultStep {
  label: string;
  status: "done" | "running" | "failed";
  message?: string;
  /** Followed through `GET /v1/actions/{id}` until the storage confirms it. */
  plan_id?: string;
}

export interface ActionResultBlock {
  type: "action_result";
  action_id: string;
  title: string;
  steps: ActionResultStep[];
}

export interface NoticeBlock {
  type: "notice";
  tone: "info" | "warning";
  text: string;
}

export type AnswerBlock =
  | FileListBlock
  | DuplicateGroupsBlock
  | StorageSummaryBlock
  | ActionProposalBlock
  | ActionResultBlock
  | NoticeBlock;

export interface ResultPage {
  total: number;
  items: AnswerFile[];
  groups: AnswerDuplicateGroup[];
}

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[];
}

export interface AskResponse {
  conversation: Conversation;
  user_message: ConversationMessage;
  assistant_message: ConversationMessage;
}
