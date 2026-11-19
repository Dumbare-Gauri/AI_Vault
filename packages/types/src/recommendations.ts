/** Mirrors apps/backend's recommendation endpoints
 * (app/presentation/api/v1/schemas.py's Recommendation and RecommendationJob response classes). */
export type RecommendationCategory =
  | "storage_optimization"
  | "knowledge_optimization"
  | "security"
  | "collaboration"
  | "productivity";

export type RecommendationRiskLevel = "low" | "medium" | "high";

export type RecommendationStatus = "active" | "resolved";

export interface Recommendation {
  id: string;
  category: RecommendationCategory;
  rule_name: string;
  title: string;
  description: string;
  confidence: number;
  estimated_impact: string;
  impact_value: number | null;
  risk_level: RecommendationRiskLevel;
  suggested_action: string;
  requires_approval: boolean;
  related_departments: string[];
  affected_file_ids: string[];
  status: RecommendationStatus;
  priority_score: number;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  /** Whether "Do it" can carry this out in storage; otherwise review-only. */
  actionable: boolean;
}

export interface RecommendationListResponse {
  items: Recommendation[];
}

export type RecommendationJobTrigger = "embedding_completed" | "manual";

export type RecommendationJobStatus = "pending" | "running" | "completed" | "failed";

export interface RecommendationJob {
  id: string;
  organization_id: string;
  triggered_by: RecommendationJobTrigger;
  status: RecommendationJobStatus;
  error: string | null;
  recommendations_active: number | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}
