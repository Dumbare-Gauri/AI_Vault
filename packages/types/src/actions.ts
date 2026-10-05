export type ActionStatus = "in_progress" | "done" | "partial" | "failed" | "undone" | "cancelled";

export interface ActionProblem {
  file_name: string;
  reason: string;
}

export interface ActionArchive {
  destination_path: string | null;
  destination_web_view_link: string | null;
  original_size_bytes: number | null;
  compressed_size_bytes: number | null;
  originals_removed_count: number;
  verified: boolean;
}

/** A storage operation the user asked for and what the provider confirmed. */
export interface Action {
  id: string;
  kind: string;
  status: ActionStatus;
  message: string;
  total: number;
  succeeded: number;
  failed: number;
  verified: number;
  provider: string;
  created_at: string;
  can_undo: boolean;
  problems: ActionProblem[];
  archive: ActionArchive | null;
}

export interface ActivityItem {
  id: string;
  kind: string;
  status: string;
  message: string;
  at: string;
}

export interface ActivityResponse {
  items: ActivityItem[];
}

export interface CreatedItem {
  id: string;
  name: string;
  path: string;
  web_view_link: string | null;
}
