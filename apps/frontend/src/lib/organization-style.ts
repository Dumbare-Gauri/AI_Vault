import type { FileLifecycleState, OrganizationRecommendationKind } from "@vault/types";

/** Takes a plain `string`, not the stricter `EntityType` — used both for
 * `OrganizationEntity.entity_type` and `FileEntityLink.entity_type`
 * (declared as `string` in files.ts to avoid that file importing from
 * organization.ts for one field), and falls back to the raw value for
 * anything unexpected rather than rejecting it. */
export function entityTypeLabel(type: string): string {
  switch (type) {
    case "project":
      return "Project";
    case "client":
      return "Client";
    case "campaign":
      return "Campaign";
    default:
      return type;
  }
}

export function recommendationKindLabel(kind: OrganizationRecommendationKind): string {
  switch (kind) {
    case "group_project":
      return "Consolidate project";
    case "group_client":
      return "Consolidate client";
    case "group_campaign":
      return "Consolidate campaign";
    case "create_folder":
      return "Create folder";
    case "move_file":
      return "Move file";
    case "move_folder":
      return "Move folder";
    case "rename_file":
      return "Rename file";
    case "rename_folder":
      return "Rename folder";
    default:
      return kind;
  }
}

export function lifecycleLabel(state: FileLifecycleState): string {
  switch (state) {
    case "keep":
      return "Keep";
    case "active":
      return "Active";
    case "reference":
      return "Reference";
    case "archive_candidate":
      return "Archive candidate";
    case "duplicate_candidate":
      return "Duplicate candidate";
    case "obsolete_candidate":
      return "Obsolete candidate";
    case "review_required":
      return "Needs review";
    case "unknown":
      return "Unknown";
    default:
      return state;
  }
}

/** Deliberately restrained, same philosophy as `recommendation-style.ts`'s
 * `categoryBadgeVariant` — no rainbow dashboards, and never a color-only
 * signal (always paired with the text label above). */
export function lifecycleBadgeVariant(
  state: FileLifecycleState,
): "primary" | "ai" | "destructive" | "warning" | "success" | "default" {
  switch (state) {
    case "active":
      return "success";
    case "reference":
    case "keep":
      return "default";
    case "archive_candidate":
    case "duplicate_candidate":
    case "obsolete_candidate":
      return "warning";
    case "review_required":
      return "destructive";
    default:
      return "default";
  }
}
