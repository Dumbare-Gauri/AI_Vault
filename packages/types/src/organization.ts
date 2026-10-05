/** Mirrors apps/backend's organization endpoints
 * (app/presentation/api/v1/schemas.py's Organization* response classes). */
export type EntityType = "project" | "client" | "campaign";

export type EntityStatus = "active" | "archived";

export interface OrganizationEntity {
  id: string;
  entity_type: EntityType;
  name: string;
  confidence: number;
  evidence: Array<{ type: string; description: string }>;
  status: EntityStatus;
  created_at: string;
  updated_at: string;
}

export interface OrganizationEntityListResponse {
  items: OrganizationEntity[];
}

export interface OrganizationEntityFile {
  id: string;
  name: string;
  path: string;
}

export interface OrganizationEntityDetail {
  entity: OrganizationEntity;
  files: OrganizationEntityFile[];
}

export type OrganizationRecommendationKind =
  | "create_folder"
  | "move_file"
  | "move_folder"
  | "rename_file"
  | "rename_folder"
  | "group_project"
  | "group_client"
  | "group_campaign";

export type OrganizationRecommendationStatus = "active" | "applied" | "rejected" | "stale";

export interface OrganizationCurrentLocation {
  folder_id: string;
  path: string | null;
  file_count: number;
}

export interface OrganizationRecommendation {
  id: string;
  kind: OrganizationRecommendationKind;
  entity_id: string | null;
  title: string;
  reasoning_summary: string;
  evidence: Array<{ type: string; description: string }>;
  confidence: number;
  affected_file_ids: string[];
  current_locations: OrganizationCurrentLocation[];
  suggested_destination: string[];
  estimated_storage_impact_bytes: number | null;
  status: OrganizationRecommendationStatus;
  execution_plan_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface OrganizationRecommendationListResponse {
  items: OrganizationRecommendation[];
}

export type OrganizationAnalysisJobStatus = "pending" | "running" | "completed" | "failed";

export interface OrganizationAnalysisJob {
  id: string;
  organization_id: string;
  status: OrganizationAnalysisJobStatus;
  clusters_found: number;
  entities_created: number;
  recommendations_generated: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}
