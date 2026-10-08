/** Mirrors apps/backend's dashboard endpoint
 * (app/presentation/api/v1/schemas.py's DashboardResponse/DashboardSnapshotResponse/
 * InsightRecordResponse). */
import type { FileSummary } from "./files";

export interface DashboardSnapshot {
  connected_providers: number;
  total_files: number;
  total_folders: number;
  total_storage_bytes: number;
  classified_files: number;
  unclassified_files: number;
  pending_enrichment_files: number;
  embedded_files: number;
  relationship_count: number;
  active_recommendations: number;
  knowledge_completeness_score: number;
  created_at: string;
}

export interface InsightRecord {
  id: string;
  insight_type: string;
  title: string;
  description: string;
  confidence: number;
  related_file_ids: string[];
  created_at: string;
}

export interface IntelligenceSummary {
  files_analyzed: number;
  /** Active project / client / campaign counts keyed by entity type. */
  entities_by_type: Record<string, number>;
  /** Lifecycle state counts, e.g. archive_candidate, review_required. */
  lifecycle_by_state: Record<string, number>;
  organize_suggestions: number;
}

export interface Dashboard {
  connector_count: number;
  latest_snapshot: DashboardSnapshot | null;
  snapshot_history: DashboardSnapshot[];
  recent_insights: InsightRecord[];
  recent_activity: FileSummary[];
  latest_scan_status: string | null;
  latest_enrichment_status: string | null;
  latest_embedding_status: string | null;
  latest_recommendation_status: string | null;
  intelligence: IntelligenceSummary;
}
