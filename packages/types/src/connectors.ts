/** Mirrors apps/backend's connector endpoints
 * (app/presentation/api/v1/schemas.py's ConnectorResponse) — never includes
 * token values, those never leave the backend. */
export interface Connector {
  id: string;
  provider: "google_workspace";
  status: "pending" | "connected" | "error" | "disconnected" | "reauth_required";
  account_email: string | null;
  workspace_domain: string | null;
  last_verified_at: string | null;
  last_failed_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  /** What users call this storage, e.g. "Google Drive". */
  provider_name: string;
  display_name: string | null;
  last_synced_at: string | null;
  /** Provider-reported; refreshed after each sync. */
  storage_used_bytes: number | null;
  /** Null when the provider reports no limit or hasn't been asked yet. */
  storage_total_bytes: number | null;
  /** Part of the used storage taken by files in the provider's Trash. */
  storage_trash_bytes: number | null;
  quota_checked_at: string | null;
}

export interface InitiateConnectResponse {
  authorize_url: string;
}

export interface CompleteConnectRequest {
  code: string;
  state: string;
}

export interface TrashItem {
  name: string;
  size_bytes: number;
  /** Whether a completed AI Vault archive already holds a copy. */
  backed_up: boolean;
}

/** What is in (or was just removed from) the connected storage's own Trash. */
export interface TrashSummary {
  file_count: number;
  total_bytes: number;
  not_backed_up_count: number;
  largest: TrashItem[];
  /** Accepted by the provider but still being removed when last checked. */
  still_deleting_count: number;
}

export interface EmptyTrashRequest {
  expected_count: number;
  confirmation: string;
}
