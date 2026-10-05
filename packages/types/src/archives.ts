/** Mirrors apps/backend's archive endpoints
 * (app/presentation/api/v1/schemas.py's ArchiveJobResponse/ArchiveJobDetailResponse). */
export type ArchiveJobStatus = "pending" | "creating" | "completed" | "failed" | "deleted";

export interface ArchiveManifestEntry {
  file_id: string;
  name: string;
  path: string;
  size_bytes: number;
  mime_type: string | null;
  checksum_sha256: string;
  provider_file_id?: string | null;
  original_size_bytes?: number | null;
  original_modified_at?: string | null;
  exported_as?: string | null;
  zip_entry_name?: string | null;
  original_removed?: boolean;
}

export interface ArchiveJob {
  id: string;
  organization_id: string;
  execution_plan_id: string;
  name: string;
  status: ArchiveJobStatus;
  object_storage_key: string | null;
  original_size_bytes: number | null;
  compressed_size_bytes: number | null;
  file_count: number;
  created_by_user_id: string;
  created_at: string;
  completed_at: string | null;
  /** Where the archive lives in the user's own connected storage. */
  destination_path: string | null;
  destination_web_view_link: string | null;
  archive_sha256: string | null;
  /** Set only once the provider confirmed the stored bytes match. */
  verified_at: string | null;
  remove_originals: boolean;
  originals_removed_count: number;
}

export interface ArchiveJobDetail extends ArchiveJob {
  manifest: ArchiveManifestEntry[];
}
